package org.acme;

import io.quarkus.hibernate.orm.panache.PanacheRepository;
import org.hibernate.Criteria;
import org.hibernate.LockMode;
import org.hibernate.Session;
import org.hibernate.query.Query;

import javax.enterprise.context.ApplicationScoped;
import javax.inject.Inject;
import javax.persistence.EntityManager;
import javax.persistence.LockModeType;
import javax.persistence.TypedQuery;
import javax.transaction.Transactional;

import java.util.List;
import java.util.Optional;

@ApplicationScoped
public class PersonRepository implements PanacheRepository<Person> {

    @Inject
    EntityManager entityManager;

    /**
     * Panache repository query.
     */
    public Optional<Person> findByEmailPanache(String email) {
        return find("email", email).firstResultOptional();
    }

    /**
     * Panache repository list query.
     */
    public List<Person> findActivePanache() {
        return list("active", true);
    }

    /**
     * Classic EntityManager + JPQL.
     */
    public List<Person> findByLastNameJpa(String lastName) {
        TypedQuery<Person> query = entityManager.createQuery(
                "select p from Person p where p.lastName = :lastName",
                Person.class
        );

        return query
                .setParameter("lastName", lastName)
                .getResultList();
    }

    /**
     * EntityManager single result.
     */
    public Person findRequiredByEmailJpa(String email) {
        return entityManager.createQuery(
                        "select p from Person p where p.email = :email",
                        Person.class
                )
                .setParameter("email", email)
                .getSingleResult();
    }

    /**
     * EntityManager native SQL.
     */
    @SuppressWarnings("unchecked")
    public List<Person> findUsingNativeQuery() {
        return entityManager
                .createNativeQuery(
                        "select * from PERSON where ACTIVE = true",
                        Person.class
                )
                .getResultList();
    }

    /**
     * EntityManager locking.
     */
    @Transactional
    public Person findAndLockJpa(Long id) {
        return entityManager.find(
                Person.class,
                id,
                LockModeType.PESSIMISTIC_WRITE
        );
    }

    /**
     * Explicit persistence lifecycle.
     */
    @Transactional
    public Person saveWithEntityManager(Person person) {
        entityManager.persist(person);
        entityManager.flush();
        entityManager.refresh(person);
        return person;
    }

    /**
     * Explicit merge.
     */
    @Transactional
    public Person updateWithMerge(Person person) {
        Person managed = entityManager.merge(person);
        entityManager.flush();
        return managed;
    }

    /**
     * Bridge from JPA to Hibernate API.
     */
    public Session getHibernateSession() {
        return entityManager.unwrap(Session.class);
    }

    /**
     * Hibernate HQL API.
     */
    public List<Person> findUsingHibernateQuery(String city) {
        Session session = entityManager.unwrap(Session.class);

        Query<Person> query = session.createQuery(
                "from Person p where p.city = :city",
                Person.class
        );

        return query
                .setParameter("city", city)
                .list();
    }

    /**
     * Hibernate-specific locking.
     */
    @Transactional
    public Person findAndLockHibernate(Long id) {
        Session session = entityManager.unwrap(Session.class);

        Person person = session.get(Person.class, id);

        if (person != null) {
            session.buildLockRequest(
                    new org.hibernate.LockOptions(LockMode.PESSIMISTIC_WRITE)
            ).lock(person);
        }

        return person;
    }

    /**
     * Intentionally legacy Hibernate Criteria API.
     *
     * This is exactly the kind of API migration we want the pipeline
     * to detect when moving from Hibernate 5.6 to Hibernate 6.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findUsingLegacyCriteria(String lastName) {
        Session session = entityManager.unwrap(Session.class);

        Criteria criteria = session.createCriteria(Person.class);

        criteria.add(
                org.hibernate.criterion.Restrictions.eq(
                        "lastName",
                        lastName
                )
        );

        return criteria.list();
    }

    /**
     * Another legacy Criteria use case with ordering and limit.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findLatestUsingLegacyCriteria() {
        Session session = entityManager.unwrap(Session.class);

        Criteria criteria = session.createCriteria(Person.class);

        criteria.add(
                org.hibernate.criterion.Restrictions.eq(
                        "active",
                        true
                )
        );

        criteria.addOrder(
                org.hibernate.criterion.Order.desc("createdAt")
        );

        criteria.setMaxResults(10);

        return criteria.list();
    }

    /**
     * Panache delete.
     */
    @Transactional
    public long deleteInactivePanache() {
        return delete("active", false);
    }

    /**
     * Direct EntityManager delete using JPQL.
     */
    @Transactional
    public int deleteInactiveJpa() {
        return entityManager
                .createQuery(
                        "delete from Person p where p.active = false"
                )
                .executeUpdate();
    }

    /**
     * Mixed Panache/JPA API.
     */
    @Transactional
    public Person saveAndFlush(Person person) {
        persist(person);
        flush();

        entityManager.refresh(person);

        return person;
    }
}