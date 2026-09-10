package org.acme;

import io.quarkus.hibernate.orm.panache.PanacheRepository;
import org.hibernate.Criteria;
import org.hibernate.LockMode;
import org.hibernate.LockOptions;
import org.hibernate.Session;
import org.hibernate.criterion.DetachedCriteria;
import org.hibernate.criterion.Example;
import org.hibernate.criterion.MatchMode;
import org.hibernate.criterion.Order;
import org.hibernate.criterion.Projections;
import org.hibernate.criterion.Restrictions;
import org.hibernate.criterion.Subqueries;
import org.hibernate.query.Query;
import org.hibernate.type.StandardBasicTypes;

import javax.enterprise.context.ApplicationScoped;
import javax.inject.Inject;
import javax.persistence.EntityManager;
import javax.transaction.Transactional;

import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.Optional;

@ApplicationScoped
public class PersonRepository implements PanacheRepository<Person> {

    @Inject
    EntityManager entityManager;

    /**
     * Bridge from JPA to the Hibernate 5 Session API.
     */
    public Session getHibernateSession() {
        return entityManager.unwrap(Session.class);
    }

    private Session session() {
        return getHibernateSession();
    }

    /**
     * Hibernate 5 classic Criteria API.
     * In Hibernate 6, Session#createCriteria and org.hibernate.Criteria are gone.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findByLastNameUsingClassicCriteria(String lastName) {
        Criteria criteria = session().createCriteria(Person.class);
        criteria.add(Restrictions.eq("lastName", lastName));
        criteria.addOrder(Order.asc("firstName"));
        return criteria.list();
    }

    /**
     * Old Hibernate 5 uniqueResult pattern.
     */
    @SuppressWarnings("deprecation")
    public Optional<Person> findByEmailUsingClassicCriteria(String email) {
        Criteria criteria = session().createCriteria(Person.class);
        criteria.add(Restrictions.eq("email", email));
        criteria.setMaxResults(1);
        return Optional.ofNullable((Person) criteria.uniqueResult());
    }

    /**
     * Criteria + MatchMode/ilike is another legacy Hibernate 5 pattern.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findActiveByCityIgnoringCase(String city) {
        Criteria criteria = session().createCriteria(Person.class);
        criteria.add(Restrictions.eq("active", true));
        criteria.add(Restrictions.ilike("city", city, MatchMode.EXACT));
        criteria.addOrder(Order.desc("createdAt"));
        return criteria.list();
    }

    /**
     * Example queries are Hibernate-5 specific and were removed from Hibernate 6.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findByExampleUsingHibernateExample(Person probe) {
        Criteria criteria = session().createCriteria(Person.class);

        Example example = Example.create(probe)
                .excludeZeroes()
                .excludeProperty("id")
                .excludeProperty("version")
                .excludeProperty("createdAt")
                .excludeProperty("active")
                .ignoreCase()
                .enableLike(MatchMode.START);

        criteria.add(example);
        criteria.addOrder(Order.asc("lastName"));
        criteria.addOrder(Order.asc("firstName"));
        return criteria.list();
    }

    /**
     * Legacy Criteria with order + limit.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findLatestActiveUsingClassicCriteria(int maxResults) {
        Criteria criteria = session().createCriteria(Person.class);
        criteria.add(Restrictions.eq("active", true));
        criteria.addOrder(Order.desc("createdAt"));
        criteria.setMaxResults(maxResults);
        return criteria.list();
    }

    /**
     * Projection API in the old Hibernate 5 Criteria style.
     */
    @SuppressWarnings("deprecation")
    public long countByLastNameUsingProjection(String lastName) {
        Criteria criteria = session().createCriteria(Person.class);
        criteria.add(Restrictions.eq("lastName", lastName));
        criteria.setProjection(Projections.rowCount());
        return (Long) criteria.uniqueResult();
    }

    /**
     * Projection + ResultTransformer is a classic Hibernate 5 migration hotspot.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Map<String, Object>> findProjectedActivePeople() {
        Criteria criteria = session().createCriteria(Person.class);
        criteria.add(Restrictions.eq("active", true));
        criteria.setProjection(
                Projections.projectionList()
                        .add(Projections.property("id"), "id")
                        .add(Projections.property("firstName"), "firstName")
                        .add(Projections.property("lastName"), "lastName")
                        .add(Projections.property("email"), "email")
                        .add(Projections.property("createdAt"), "createdAt")
        );
        criteria.setResultTransformer(Criteria.ALIAS_TO_ENTITY_MAP);
        return criteria.list();
    }

    /**
     * SQLRestriction is another old Hibernate-only feature.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findByEmailDomainUsingSqlRestriction(String emailDomain) {
        Criteria criteria = session().createCriteria(Person.class);
        criteria.add(
                Restrictions.sqlRestriction(
                        "lower({alias}.email) like ?",
                        "%@" + emailDomain.toLowerCase(),
                        StandardBasicTypes.STRING
                )
        );
        criteria.addOrder(Order.asc("email"));
        return criteria.list();
    }

    /**
     * DetachedCriteria + Subqueries are strong examples of Hibernate-5 specific query code.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findPeopleCreatedAtLatestTimestamp() {
        DetachedCriteria latestCreatedAt = DetachedCriteria.forClass(Person.class)
                .setProjection(Projections.max("createdAt"));

        Criteria criteria = session().createCriteria(Person.class);
        criteria.add(Subqueries.propertyEq("createdAt", latestCreatedAt));
        return criteria.list();
    }

    /**
     * HQL over the native Hibernate Session.
     */
    public List<Person> findCreatedAfterUsingHibernateQuery(Instant createdAfter) {
        Query<Person> query = session().createQuery(
                "from Person p where p.createdAt >= :createdAfter order by p.createdAt desc",
                Person.class
        );

        return query
                .setParameter("createdAfter", createdAfter)
                .list();
    }

    /**
     * Explicit locking via Hibernate Session/LockOptions.
     */
    @Transactional
    public Person findAndLockHibernate(Long id) {
        Person person = session().get(Person.class, id);

        if (person != null) {
            session().buildLockRequest(new LockOptions(LockMode.PESSIMISTIC_WRITE)).lock(person);
        }

        return person;
    }

    /**
     * saveOrUpdate is intentionally Hibernate-centric.
     */
    @Transactional
    public Person saveWithHibernateSession(Person person) {
        session().saveOrUpdate(person);
        session().flush();
        return person;
    }

    /**
     * Hibernate HQL bulk update.
     */
    @Transactional
    public int deactivateByCityUsingBulkHql(String city) {
        Query<?> query = session().createQuery(
                "update Person p set p.active = false where p.city = :city"
        );

        return query
                .setParameter("city", city)
                .executeUpdate();
    }

    /**
     * Hibernate HQL bulk delete.
     */
    @Transactional
    public int deleteInactiveUsingBulkHql() {
        Query<?> query = session().createQuery(
                "delete from Person p where p.active = false"
        );

        return query.executeUpdate();
    }
}