package org.acme;

import io.quarkus.hibernate.orm.panache.PanacheRepository;
import javax.enterprise.context.ApplicationScoped;
import javax.inject.Inject;
import javax.persistence.EntityManager;
import javax.transaction.Transactional;
import org.hibernate.LockMode;
import org.hibernate.LockOptions;
import org.hibernate.Session;
import org.hibernate.criterion.DetachedCriteria;
import org.hibernate.criterion.Example;
import org.hibernate.criterion.MatchMode;
import org.hibernate.criterion.Order;
import org.hibernate.criterion.Projections;
import org.hibernate.criterion.Property;
import org.hibernate.criterion.Restrictions;
import org.hibernate.transform.Transformers;
import org.hibernate.type.StandardBasicTypes;

import java.time.Instant;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;

/**
 * Hibernate 5.6 baseline with legacy APIs for migration analysis.
 */
@ApplicationScoped
@SuppressWarnings("unchecked")
public class PersonRepository implements PanacheRepository<Person> {

    @Inject
    EntityManager entityManager;

    public Session getHibernateSession() {
        return entityManager.unwrap(Session.class);
    }

    private Session session() {
        return getHibernateSession();
    }

    public List<Person> findByLastNameUsingClassicCriteria(String lastName) {
        return session().createCriteria(Person.class)
                .add(Restrictions.eq("lastName", lastName))
                .addOrder(Order.asc("firstName"))
                .list();
    }

    public Optional<Person> findByEmailUsingClassicCriteria(String email) {
        return Optional.ofNullable((Person) session().createCriteria(Person.class)
                .add(Restrictions.eq("email", email))
                .setMaxResults(1)
                .uniqueResult());
    }

    public List<Person> findActiveByCityIgnoringCase(String city) {
        return session().createCriteria(Person.class)
                .add(Restrictions.eq("active", true))
                .add(Restrictions.ilike("city", city, MatchMode.EXACT))
                .addOrder(Order.desc("createdAt"))
                .list();
    }

    /**
     * Matches non-null string properties by case-insensitive prefix.
     * Ignore the probe's default timestamp and active flag; identifiers and
     * version properties are already ignored by Hibernate Example.
     */
    public List<Person> findByExampleUsingHibernateExample(Person probe) {
        Example example = Example.create(probe)
                .excludeProperty("createdAt")
                .excludeProperty("active")
                .ignoreCase()
                .enableLike(MatchMode.START);
        return session().createCriteria(Person.class)
                .add(example)
                .addOrder(Order.asc("lastName"))
                .addOrder(Order.asc("firstName"))
                .list();
    }

    public List<Person> findLatestActiveUsingClassicCriteria(int maxResults) {
        return session().createCriteria(Person.class)
                .add(Restrictions.eq("active", true))
                .addOrder(Order.desc("createdAt"))
                .setMaxResults(maxResults)
                .list();
    }

    public long countByLastNameUsingProjection(String lastName) {
        return ((Number) session().createCriteria(Person.class)
                .add(Restrictions.eq("lastName", lastName))
                .setProjection(Projections.rowCount())
                .uniqueResult()).longValue();
    }

    public List<Map<String, Object>> findProjectedActivePeople() {
        return session().createCriteria(Person.class)
                .add(Restrictions.eq("active", true))
                .setProjection(Projections.projectionList()
                        .add(Projections.property("id"), "id")
                        .add(Projections.property("firstName"), "firstName")
                        .add(Projections.property("lastName"), "lastName")
                        .add(Projections.property("email"), "email")
                        .add(Projections.property("createdAt"), "createdAt"))
                .setResultTransformer(Transformers.ALIAS_TO_ENTITY_MAP)
                .list();
    }

    public List<Person> findByEmailDomainUsingSqlRestriction(String emailDomain) {
        return session().createCriteria(Person.class)
                .add(Restrictions.sqlRestriction("lower({alias}.email) like ?",
                        "%@" + emailDomain.toLowerCase(Locale.ROOT), StandardBasicTypes.STRING))
                .addOrder(Order.asc("email"))
                .list();
    }

    public List<Person> findPeopleCreatedAtLatestTimestamp() {
        DetachedCriteria latestTimestamp = DetachedCriteria.forClass(Person.class)
                .setProjection(Projections.max("createdAt"));
        return session().createCriteria(Person.class)
                .add(Property.forName("createdAt").eq(latestTimestamp))
                .list();
    }

    public List<Person> findCreatedAfterUsingHibernateQuery(Instant createdAfter) {
        return session().createQuery(
                "from Person p where p.createdAt >= :createdAfter order by p.createdAt desc", Person.class)
                .setParameter("createdAfter", createdAfter)
                .getResultList();
    }

    @Transactional
    public Person findAndLockHibernate(Long id) {
        Person person = session().get(Person.class, id);
        if (person != null) {
            session().buildLockRequest(new LockOptions(LockMode.PESSIMISTIC_WRITE)).lock(person);
        }
        return person;
    }

    @Transactional
    public Person saveWithHibernateSession(Person person) {
        session().saveOrUpdate(person);
        session().flush();
        return person;
    }

    @Transactional
    public int deactivateByCityUsingBulkHql(String city) {
        return session().createQuery("update Person p set p.active = false where p.city = :city")
                .setParameter("city", city)
                .executeUpdate();
    }

    @Transactional
    public int deleteInactiveUsingBulkHql() {
        return session().createQuery("delete from Person p where p.active = false")
                .executeUpdate();
    }
}
